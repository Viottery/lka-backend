"""Deterministic benchmark metrics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from evals.lka_evals.log_parser import REQUIRED_LOG_SECTIONS
from evals.lka_evals.subject import EvalRunArtifact


@dataclass
class MetricResult:
    name: str
    score: float
    passed: bool
    details: dict[str, Any]
    weight: float = 1.0


def evaluate_case(case: dict[str, Any], artifact: EvalRunArtifact) -> list[MetricResult]:
    expected = case.get("expect")
    expected = expected if isinstance(expected, dict) else {}
    metrics: list[MetricResult] = []

    metrics.append(_no_runtime_error(artifact))
    if "selected_package" in expected:
        metrics.append(_selected_package_match(artifact, expected.get("selected_package")))
    if "tool_sequence" in expected:
        metrics.append(_tool_sequence_exact_match(artifact, expected.get("tool_sequence")))
        metrics.append(_tool_sequence_f1(artifact, expected.get("tool_sequence")))
    if "required_tools" in expected:
        metrics.append(_required_tools_called(artifact, expected.get("required_tools")))
    if "forbidden_tools" in expected:
        metrics.append(_forbidden_tools_not_called(artifact, expected.get("forbidden_tools")))
    if "answer_contains_all" in expected:
        metrics.append(_answer_contains_all(artifact, expected.get("answer_contains_all")))
    if "answer_contains_any" in expected:
        metrics.append(_answer_contains_any(artifact, expected.get("answer_contains_any")))
    if "answer_excludes_all" in expected:
        metrics.append(_answer_excludes_all(artifact, expected.get("answer_excludes_all")))
    if "output_contains_all" in expected:
        metrics.append(_output_contains_all(artifact, expected.get("output_contains_all")))
    if "output_equals" in expected:
        metrics.append(_output_equals(artifact, expected.get("output_equals")))
    if "capabilities_include" in expected:
        metrics.append(_capabilities_include(artifact, expected.get("capabilities_include")))
    if "workspace_min_files" in expected:
        metrics.append(_workspace_min_files(artifact, expected.get("workspace_min_files")))
    if "workspace_min_chunks" in expected:
        metrics.append(_workspace_min_chunks(artifact, expected.get("workspace_min_chunks")))
    if "knowledge_relevant_titles" in expected:
        metrics.append(_knowledge_search_recall(artifact, expected.get("knowledge_relevant_titles")))
    if "knowledge_forbidden_titles" in expected:
        metrics.append(_knowledge_search_precision(artifact, expected.get("knowledge_forbidden_titles")))
    if "knowledge_required_chunk_titles" in expected:
        metrics.append(_knowledge_loaded_evidence(artifact, expected.get("knowledge_required_chunk_titles")))
    if "knowledge_answer_contains_all" in expected:
        metrics.append(_knowledge_answer_contains(artifact, expected.get("knowledge_answer_contains_all")))
    if "answer_exact" in expected or "answer_f1" in expected:
        metrics.append(_answer_quality(artifact, expected))
    if "tool_output_contains_all" in expected:
        metrics.append(_tool_output_contains_all(artifact, expected.get("tool_output_contains_all")))
    if "tool_output_excludes_all" in expected:
        metrics.append(_tool_output_excludes_all(artifact, expected.get("tool_output_excludes_all")))
    if "tool_statuses" in expected:
        metrics.append(_tool_statuses_match(artifact, expected.get("tool_statuses")))
    if "filesystem_snapshot_contains_all" in expected:
        metrics.append(
            _filesystem_snapshot_contains_all(
                artifact,
                expected.get("filesystem_snapshot_contains_all"),
            )
        )
    if "filesystem_snapshot_excludes_all" in expected:
        metrics.append(
            _filesystem_snapshot_excludes_all(
                artifact,
                expected.get("filesystem_snapshot_excludes_all"),
            )
        )
    if "safety_review_count" in expected:
        metrics.append(_safety_review_count(artifact, expected.get("safety_review_count")))
    if "safety_review_tools" in expected:
        metrics.append(_safety_review_tools(artifact, expected.get("safety_review_tools")))
    if "safety_review_statuses" in expected:
        metrics.append(_safety_review_statuses(artifact, expected.get("safety_review_statuses")))
    if "safety_review_modes" in expected:
        metrics.append(_safety_review_modes(artifact, expected.get("safety_review_modes")))
    if "evidence_external_ids" in expected:
        metrics.append(_evidence_message_recall(artifact, expected.get("evidence_external_ids")))
        metrics.append(_loaded_required_messages(artifact, expected.get("evidence_external_ids")))
    mail_search_relevant = expected.get(
        "mail_search_relevant_external_ids",
        expected.get("evidence_external_ids"),
    )
    if mail_search_relevant is not None:
        metrics.append(_mail_search_recall_at_k(artifact, mail_search_relevant, expected))
        metrics.append(_mail_search_precision_at_k(artifact, mail_search_relevant, expected))
        metrics.append(_mail_search_mrr(artifact, mail_search_relevant, expected))
    mail_search_forbidden = expected.get(
        "mail_search_forbidden_external_ids",
        expected.get("forbidden_evidence_external_ids"),
    )
    if mail_search_forbidden is not None:
        metrics.append(_mail_search_forbidden_at_k(artifact, mail_search_forbidden, expected))
    if "forbidden_evidence_external_ids" in expected:
        metrics.append(
            _forbidden_evidence_not_loaded(
                artifact,
                expected.get("forbidden_evidence_external_ids"),
            )
        )
    if "created_matter_contains" in expected:
        metrics.append(_created_matter_contains(artifact, expected.get("created_matter_contains")))
    if "matter_source_external_ids" in expected:
        metrics.append(
            _matter_source_link_recall(artifact, expected.get("matter_source_external_ids"))
        )

    metrics.extend(
        [
            _tool_success_rate(artifact, expected.get("min_tool_success_rate")),
            _schema_rejection_count(artifact, expected.get("max_schema_rejections", 0)),
            _llm_call_count(artifact, expected.get("max_llm_calls")),
            _reported_token_total(artifact, expected.get("max_total_tokens")),
            _wall_time(artifact, expected.get("max_wall_time_ms")),
        ]
    )
    if artifact.result.get("log_path") or expected.get("requires_log"):
        metrics.append(_log_sections_complete(artifact))
    if artifact.subject == "stream" or expected.get("stream"):
        metrics.append(_sse_sequence_valid(artifact))
    return metrics


def summarize_metrics(metrics: list[MetricResult]) -> dict[str, Any]:
    weighted_total = sum(metric.weight for metric in metrics)
    weighted_score = sum(metric.score * metric.weight for metric in metrics)
    return {
        "score": round(weighted_score / weighted_total, 4) if weighted_total else 0.0,
        "passed": all(metric.passed for metric in metrics),
        "metrics": [asdict(metric) for metric in metrics],
    }


def _no_runtime_error(artifact: EvalRunArtifact) -> MetricResult:
    return MetricResult(
        name="no_runtime_error",
        score=0.0 if artifact.error else 1.0,
        passed=artifact.error is None,
        details={"error": artifact.error},
        weight=2.0,
    )


def _selected_package_match(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    actual = artifact.result.get("selected_package")
    passed = actual == expected
    return MetricResult(
        name="selected_package_match",
        score=1.0 if passed else 0.0,
        passed=passed,
        details={"expected": expected, "actual": actual},
    )


def _tool_sequence_exact_match(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_list = _string_list(expected)
    actual = _tool_names(artifact)
    passed = actual == expected_list
    return MetricResult(
        name="tool_sequence_exact_match",
        score=1.0 if passed else 0.0,
        passed=passed,
        details={"expected": expected_list, "actual": actual},
        weight=2.0,
    )


def _tool_sequence_f1(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_list = _string_list(expected)
    actual = _tool_names(artifact)
    if not expected_list and not actual:
        score = 1.0
    else:
        expected_counts = _counts(expected_list)
        actual_counts = _counts(actual)
        overlap = sum(min(expected_counts.get(key, 0), actual_counts.get(key, 0)) for key in expected_counts)
        precision = overlap / len(actual) if actual else 0.0
        recall = overlap / len(expected_list) if expected_list else 0.0
        score = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return MetricResult(
        name="tool_sequence_f1",
        score=round(score, 4),
        passed=score >= 0.999,
        details={"expected": expected_list, "actual": actual},
    )


def _required_tools_called(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    required = set(_string_list(expected))
    actual = set(_tool_names(artifact))
    missing = sorted(required - actual)
    return MetricResult(
        name="required_tool_called",
        score=1.0 - (len(missing) / len(required)) if required else 1.0,
        passed=not missing,
        details={"missing": missing, "actual": sorted(actual)},
    )


def _forbidden_tools_not_called(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    forbidden = set(_string_list(expected))
    actual = set(_tool_names(artifact))
    violations = sorted(forbidden & actual)
    return MetricResult(
        name="forbidden_tool_not_called",
        score=0.0 if violations else 1.0,
        passed=not violations,
        details={"violations": violations},
    )


def _answer_contains_all(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    required = _string_list(expected)
    answer = str(artifact.result.get("answer") or "")
    missing = [item for item in required if item not in answer]
    return MetricResult(
        name="answer_contains_all",
        score=1.0 - (len(missing) / len(required)) if required else 1.0,
        passed=not missing,
        details={"missing": missing},
        weight=2.0,
    )


def _answer_contains_any(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    candidates = _string_list(expected)
    answer = str(artifact.result.get("answer") or "")
    matched = [item for item in candidates if item in answer]
    return MetricResult(
        name="answer_contains_any",
        score=1.0 if matched or not candidates else 0.0,
        passed=bool(matched) or not candidates,
        details={"matched": matched, "candidates": candidates},
    )


def _answer_excludes_all(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    forbidden = _string_list(expected)
    answer = str(artifact.result.get("answer") or "")
    violations = [item for item in forbidden if item in answer]
    return MetricResult(
        name="answer_excludes_all",
        score=0.0 if violations else 1.0,
        passed=not violations,
        details={"violations": violations},
    )


def _output_contains_all(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    required = _string_list(expected)
    text = _result_text(artifact)
    missing = [item for item in required if item not in text]
    return MetricResult(
        name="output_contains_all",
        score=1.0 - (len(missing) / len(required)) if required else 1.0,
        passed=not missing,
        details={"missing": missing},
        weight=2.0,
    )


def _output_equals(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    output = artifact.result.get("output")
    passed = output == expected
    return MetricResult(
        name="output_equals",
        score=1.0 if passed else 0.0,
        passed=passed,
        details={"expected": expected, "actual": output},
    )


def _capabilities_include(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    required = set(_string_list(expected))
    output = artifact.result.get("output")
    capabilities = output.get("capabilities") if isinstance(output, dict) else None
    names: set[str] = set()
    if isinstance(capabilities, list):
        names = {
            str(capability.get("name"))
            for capability in capabilities
            if isinstance(capability, dict) and capability.get("name")
        }
    missing = sorted(required - names)
    return MetricResult(
        name="capabilities_include",
        score=1.0 - (len(missing) / len(required)) if required else 1.0,
        passed=not missing,
        details={"missing": missing, "actual": sorted(names)},
    )


def _workspace_min_files(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    minimum = int(expected) if isinstance(expected, int) else 0
    output = artifact.result.get("output")
    actual = output.get("indexed_files") if isinstance(output, dict) else None
    actual = int(actual) if isinstance(actual, int) else 0
    return MetricResult(
        name="workspace_min_files",
        score=1.0 if actual >= minimum else 0.0,
        passed=actual >= minimum,
        details={"minimum": minimum, "actual": actual},
    )


def _workspace_min_chunks(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    minimum = int(expected) if isinstance(expected, int) else 0
    output = artifact.result.get("output")
    actual = output.get("indexed_chunks") if isinstance(output, dict) else None
    actual = int(actual) if isinstance(actual, int) else 0
    return MetricResult(
        name="workspace_min_chunks",
        score=1.0 if actual >= minimum else 0.0,
        passed=actual >= minimum,
        details={"minimum": minimum, "actual": actual},
    )


def _knowledge_search_events(artifact: EvalRunArtifact) -> list[dict[str, Any]]:
    return [event for event in artifact.result.get("tool_events", [])
            if isinstance(event, dict) and event.get("tool_name") == "knowledge.search"]


def _knowledge_search_items(artifact: EvalRunArtifact) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if artifact.result.get("operation") == "knowledge_search":
        output = artifact.result.get("output", {})
        return output.get("results", []) if isinstance(output, dict) else []
    for event in _knowledge_search_events(artifact):
        result = event.get("result", {})
        output = result.get("output", {}) if isinstance(result, dict) else {}
        if isinstance(output, dict) and isinstance(output.get("results"), list):
            items.extend(item for item in output["results"] if isinstance(item, dict))
    return items


def _knowledge_search_recall(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_titles = {str(item) for item in expected} if isinstance(expected, list) else set()
    found_titles = {str(item.get("title")) for item in _knowledge_search_items(artifact)}
    missing = sorted(expected_titles - found_titles)
    score = len(expected_titles & found_titles) / len(expected_titles) if expected_titles else 1.0
    return MetricResult("knowledge_search_recall", score, not missing, {"expected_titles": sorted(expected_titles), "found_titles": sorted(found_titles), "missing": missing})


def _knowledge_search_precision(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    forbidden = {str(item) for item in expected} if isinstance(expected, list) else set()
    found = [str(item.get("title")) for item in _knowledge_search_items(artifact)]
    violations = sorted(forbidden & set(found))
    score = 1.0 if not found else (len(set(found) - forbidden) / len(found))
    return MetricResult("knowledge_search_precision", score, not violations, {"retrieved_titles": found, "forbidden_titles": sorted(forbidden), "violations": violations})


def _knowledge_loaded_evidence(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    required = {str(item) for item in expected} if isinstance(expected, list) else set()
    loaded: set[str] = set()
    for event in artifact.result.get("tool_events", []):
        if not isinstance(event, dict) or event.get("tool_name") != "knowledge.load_chunks":
            continue
        output = (event.get("result") or {}).get("output", {})
        for chunk in output.get("chunks", []) if isinstance(output, dict) else []:
            if isinstance(chunk, dict):
                loaded.add(str(chunk.get("title")))
    missing = sorted(required - loaded)
    score = len(required & loaded) / len(required) if required else 1.0
    return MetricResult("knowledge_evidence_recall", score, not missing, {"required_titles": sorted(required), "loaded_titles": sorted(loaded), "missing": missing})


def _knowledge_answer_contains(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    answer = str(artifact.result.get("answer") or "").casefold()
    terms = [str(item) for item in expected] if isinstance(expected, list) else []
    missing = [term for term in terms if term.casefold() not in answer]
    score = (len(terms) - len(missing)) / len(terms) if terms else 1.0
    return MetricResult("knowledge_answer_fact_accuracy", score, not missing, {"missing_terms": missing, "checked_terms": terms})


def _answer_quality(artifact: EvalRunArtifact, expected: dict[str, Any]) -> MetricResult:
    actual = _normalize_answer(str(artifact.result.get("answer") or ""))
    gold = _normalize_answer(str(expected.get("answer_exact") or expected.get("answer_f1") or ""))
    actual_tokens = actual.split(); gold_tokens = gold.split()
    exact = float(actual == gold and bool(gold))
    common = sum(min(actual_tokens.count(t), gold_tokens.count(t)) for t in set(gold_tokens))
    precision = common / len(actual_tokens) if actual_tokens else 0.0
    recall = common / len(gold_tokens) if gold_tokens else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    minimum = expected.get("answer_min_f1", 1.0 if "answer_f1" in expected else None)
    passed = exact >= 1.0 if "answer_exact" in expected else (minimum is None or f1 >= float(minimum))
    return MetricResult("answer_quality", round(exact if "answer_exact" in expected else f1, 4), passed, {"exact_match": exact, "f1": round(f1,4), "gold": gold, "actual_preview": actual[:500], "minimum_f1": minimum}, weight=2.0)


def _normalize_answer(value: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.casefold())).strip()


def _tool_output_contains_all(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    required = _string_list(expected)
    text = " ".join(
        _json_text(event.get("result"))
        for event in artifact.result.get("tool_events", [])
        if isinstance(event, dict)
    )
    missing = [item for item in required if item not in text]
    return MetricResult(
        name="tool_output_contains_all",
        score=1.0 - (len(missing) / len(required)) if required else 1.0,
        passed=not missing,
        details={"missing": missing},
        weight=2.0,
    )


def _tool_output_excludes_all(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    forbidden = _string_list(expected)
    text = _tool_events_text(artifact)
    violations = [item for item in forbidden if item in text]
    return MetricResult(
        name="tool_output_excludes_all",
        score=0.0 if violations else 1.0,
        passed=not violations,
        details={"violations": violations},
        weight=2.0,
    )


def _tool_statuses_match(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_list = _string_list(expected)
    actual = []
    for event in artifact.result.get("tool_events", []):
        if not isinstance(event, dict):
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        actual.append(str(result.get("status") or ""))
    passed = actual == expected_list
    return MetricResult(
        name="tool_statuses_match",
        score=_sequence_score(actual, expected_list),
        passed=passed,
        details={"expected": expected_list, "actual": actual},
        weight=2.0,
    )


def _filesystem_snapshot_contains_all(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    required = _string_list(expected)
    text = _filesystem_snapshot_text(artifact)
    missing = [item for item in required if item not in text]
    return MetricResult(
        name="filesystem_snapshot_contains_all",
        score=1.0 - (len(missing) / len(required)) if required else 1.0,
        passed=not missing,
        details={"missing": missing},
        weight=2.0,
    )


def _filesystem_snapshot_excludes_all(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    forbidden = _string_list(expected)
    text = _filesystem_snapshot_text(artifact)
    violations = [item for item in forbidden if item in text]
    return MetricResult(
        name="filesystem_snapshot_excludes_all",
        score=0.0 if violations else 1.0,
        passed=not violations,
        details={"violations": violations},
        weight=2.0,
    )


def _safety_review_count(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_count = int(expected) if isinstance(expected, int) else 0
    actual = len(_safety_reviews(artifact))
    return MetricResult(
        name="safety_review_count",
        score=1.0 if actual == expected_count else 0.0,
        passed=actual == expected_count,
        details={"expected": expected_count, "actual": actual},
    )


def _safety_review_tools(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_list = _string_list(expected)
    actual = [
        str(review.get("tool_name"))
        for review in _safety_reviews(artifact)
        if review.get("tool_name")
    ]
    return MetricResult(
        name="safety_review_tools",
        score=_sequence_score(actual, expected_list),
        passed=actual == expected_list,
        details={"expected": expected_list, "actual": actual},
    )


def _safety_review_statuses(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_list = _string_list(expected)
    actual = [
        str(review.get("status"))
        for review in _safety_reviews(artifact)
        if review.get("status")
    ]
    return MetricResult(
        name="safety_review_statuses",
        score=_sequence_score(actual, expected_list),
        passed=actual == expected_list,
        details={"expected": expected_list, "actual": actual},
    )


def _safety_review_modes(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_list = _string_list(expected)
    actual = [
        str(review.get("mode"))
        for review in _safety_reviews(artifact)
        if review.get("mode")
    ]
    return MetricResult(
        name="safety_review_modes",
        score=_sequence_score(actual, expected_list),
        passed=actual == expected_list,
        details={"expected": expected_list, "actual": actual},
    )


def _evidence_message_recall(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_ids = _expected_message_ids(artifact, expected)
    seen = set()
    for event in artifact.result.get("tool_events", []):
        if not isinstance(event, dict):
            continue
        result = event.get("result")
        if not isinstance(result, dict):
            continue
        output = result.get("output")
        if not isinstance(output, dict):
            continue
        for message in output.get("messages", []):
            if isinstance(message, dict) and message.get("message_id"):
                seen.add(str(message["message_id"]))
    missing = sorted(set(expected_ids) - seen)
    recall = 1.0 - (len(missing) / len(expected_ids)) if expected_ids else 1.0
    return MetricResult(
        name="evidence_recall_at_k",
        score=round(recall, 4),
        passed=not missing,
        details={"expected_message_ids": expected_ids, "missing": missing},
        weight=2.0,
    )


def _loaded_required_messages(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_ids = set(_expected_message_ids(artifact, expected))
    loaded = set()
    for event in artifact.result.get("tool_events", []):
        if isinstance(event, dict) and event.get("tool_name") == "mail.load_messages":
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            output = result.get("output") if isinstance(result, dict) else {}
            for message in output.get("messages", []) if isinstance(output, dict) else []:
                if isinstance(message, dict) and message.get("message_id"):
                    loaded.add(str(message["message_id"]))
    missing = sorted(expected_ids - loaded)
    return MetricResult(
        name="loaded_required_messages",
        score=1.0 - (len(missing) / len(expected_ids)) if expected_ids else 1.0,
        passed=not missing,
        details={"missing": missing, "loaded": sorted(loaded)},
    )


def _forbidden_evidence_not_loaded(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    forbidden_ids = set(_expected_message_ids(artifact, expected))
    loaded = _loaded_message_ids(artifact)
    violations = sorted(forbidden_ids & loaded)
    return MetricResult(
        name="forbidden_evidence_not_loaded",
        score=0.0 if violations else 1.0,
        passed=not violations,
        details={"violations": violations, "loaded": sorted(loaded)},
        weight=2.0,
    )


def _mail_search_recall_at_k(
    artifact: EvalRunArtifact,
    expected: Any,
    metric_options: dict[str, Any],
) -> MetricResult:
    expected_ids = set(_expected_message_ids(artifact, expected))
    retrieved = _mail_search_message_ids_at_k(artifact, _mail_search_k(metric_options))
    retrieved_set = set(retrieved)
    missing = sorted(expected_ids - retrieved_set)
    recall = len(expected_ids & retrieved_set) / len(expected_ids) if expected_ids else 1.0
    return MetricResult(
        name="mail_search_recall_at_k",
        score=round(recall, 4),
        passed=not missing,
        details={
            "k": _mail_search_k(metric_options),
            "expected_message_ids": sorted(expected_ids),
            "retrieved_message_ids": retrieved,
            "missing": missing,
            "search_event_count": _mail_search_event_count(artifact),
        },
        weight=2.0,
    )


def _mail_search_precision_at_k(
    artifact: EvalRunArtifact,
    expected: Any,
    metric_options: dict[str, Any],
) -> MetricResult:
    expected_ids = set(_expected_message_ids(artifact, expected))
    retrieved = _mail_search_message_ids_at_k(artifact, _mail_search_k(metric_options))
    retrieved_set = set(retrieved)
    relevant_count = len(expected_ids & retrieved_set)
    precision = relevant_count / len(retrieved) if retrieved else (1.0 if not expected_ids else 0.0)
    minimum = metric_options.get("mail_search_min_precision_at_k")
    passed = True if minimum is None else precision >= float(minimum)
    return MetricResult(
        name="mail_search_precision_at_k",
        score=round(precision, 4),
        passed=passed,
        details={
            "k": _mail_search_k(metric_options),
            "minimum": minimum,
            "expected_message_ids": sorted(expected_ids),
            "retrieved_message_ids": retrieved,
            "relevant_count": relevant_count,
            "retrieved_count": len(retrieved),
            "search_event_count": _mail_search_event_count(artifact),
        },
    )


def _mail_search_mrr(
    artifact: EvalRunArtifact,
    expected: Any,
    metric_options: dict[str, Any],
) -> MetricResult:
    expected_ids = set(_expected_message_ids(artifact, expected))
    retrieved = _mail_search_message_ids_at_k(artifact, _mail_search_k(metric_options))
    first_rank = None
    for index, message_id in enumerate(retrieved, start=1):
        if message_id in expected_ids:
            first_rank = index
            break
    mrr = 1.0 / first_rank if first_rank else 0.0
    minimum = metric_options.get("mail_search_min_mrr")
    passed = True if minimum is None else mrr >= float(minimum)
    return MetricResult(
        name="mail_search_mrr",
        score=round(mrr, 4),
        passed=passed,
        details={
            "k": _mail_search_k(metric_options),
            "minimum": minimum,
            "first_relevant_rank": first_rank,
            "expected_message_ids": sorted(expected_ids),
            "retrieved_message_ids": retrieved,
            "search_event_count": _mail_search_event_count(artifact),
        },
    )


def _mail_search_forbidden_at_k(
    artifact: EvalRunArtifact,
    expected: Any,
    metric_options: dict[str, Any],
) -> MetricResult:
    forbidden_ids = set(_expected_message_ids(artifact, expected))
    retrieved = _mail_search_message_ids_at_k(artifact, _mail_search_k(metric_options))
    violations = sorted(forbidden_ids & set(retrieved))
    score = 1.0 - (len(violations) / len(forbidden_ids)) if forbidden_ids else 1.0
    return MetricResult(
        name="mail_search_forbidden_at_k",
        score=round(score, 4),
        passed=not violations,
        details={
            "k": _mail_search_k(metric_options),
            "forbidden_message_ids": sorted(forbidden_ids),
            "retrieved_message_ids": retrieved,
            "violations": violations,
            "search_event_count": _mail_search_event_count(artifact),
        },
        weight=2.0,
    )


def _created_matter_contains(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    required = _string_list(expected)
    created_payloads: list[dict[str, Any]] = []
    for event in artifact.result.get("tool_events", []):
        if not isinstance(event, dict) or event.get("tool_name") != "matter.create":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        output = result.get("output") if isinstance(result, dict) else {}
        matter = output.get("matter") if isinstance(output, dict) else None
        if isinstance(matter, dict):
            created_payloads.append(matter)
    text = " ".join(str(payload) for payload in created_payloads)
    missing = [item for item in required if item not in text]
    return MetricResult(
        name="matter_write_precision",
        score=1.0 - (len(missing) / len(required)) if required else 1.0,
        passed=not missing and bool(created_payloads),
        details={"missing": missing, "created_count": len(created_payloads)},
        weight=2.0,
    )


def _matter_source_link_recall(artifact: EvalRunArtifact, expected: Any) -> MetricResult:
    expected_ids = set(_expected_message_ids(artifact, expected))
    linked_ids: set[str] = set()
    for event in artifact.result.get("tool_events", []):
        if not isinstance(event, dict) or event.get("tool_name") != "matter.create":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        output = result.get("output") if isinstance(result, dict) else {}
        matter = output.get("matter") if isinstance(output, dict) else None
        if not isinstance(matter, dict):
            continue
        source_links = matter.get("source_links")
        if not isinstance(source_links, list):
            continue
        for link in source_links:
            if (
                isinstance(link, dict)
                and link.get("source_type") == "mail_message"
                and link.get("source_id")
            ):
                linked_ids.add(str(link["source_id"]))
    missing = sorted(expected_ids - linked_ids)
    recall = 1.0 - (len(missing) / len(expected_ids)) if expected_ids else 1.0
    return MetricResult(
        name="matter_source_link_recall",
        score=round(recall, 4),
        passed=not missing,
        details={"expected_message_ids": sorted(expected_ids), "missing": missing},
        weight=2.0,
    )


def _tool_success_rate(artifact: EvalRunArtifact, minimum: Any) -> MetricResult:
    events = [event for event in artifact.result.get("tool_events", []) if isinstance(event, dict)]
    if not events:
        return MetricResult(
            name="tool_success_rate",
            score=1.0,
            passed=True,
            details={"tool_event_count": 0},
        )
    completed = 0
    for event in events:
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("status") == "completed":
            completed += 1
    score = completed / len(events)
    minimum_rate = float(minimum) if isinstance(minimum, int | float) else 1.0
    return MetricResult(
        name="tool_success_rate",
        score=round(score, 4),
        passed=score >= minimum_rate,
        details={"completed": completed, "total": len(events), "minimum": minimum_rate},
    )


def _schema_rejection_count(artifact: EvalRunArtifact, maximum: Any) -> MetricResult:
    maximum_count = int(maximum) if isinstance(maximum, int) else 0
    rejected = 0
    for event in artifact.result.get("tool_events", []):
        if not isinstance(event, dict):
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        output = result.get("output") if isinstance(result, dict) else {}
        if (
            result.get("status") == "rejected"
            and isinstance(output, dict)
            and output.get("validation_errors")
        ):
            rejected += 1
    return MetricResult(
        name="schema_rejection_count",
        score=1.0 if rejected <= maximum_count else 0.0,
        passed=rejected <= maximum_count,
        details={"actual": rejected, "maximum": maximum_count},
    )


def _llm_call_count(artifact: EvalRunArtifact, maximum: Any) -> MetricResult:
    count = len([event for event in artifact.result.get("llm_events", []) if isinstance(event, dict)])
    passed = True if maximum is None else count <= int(maximum)
    return MetricResult(
        name="llm_call_count",
        score=1.0 if passed else 0.0,
        passed=passed,
        details={"actual": count, "maximum": maximum},
    )


def _reported_token_total(artifact: EvalRunArtifact, maximum: Any) -> MetricResult:
    total = 0
    for event in artifact.result.get("llm_events", []):
        if isinstance(event, dict) and isinstance(event.get("total_token_count"), int):
            total += int(event["total_token_count"])
    passed = True if maximum is None else total <= int(maximum)
    return MetricResult(
        name="reported_token_total",
        score=1.0 if passed else 0.0,
        passed=passed,
        details={"actual": total, "maximum": maximum},
    )


def _wall_time(artifact: EvalRunArtifact, maximum: Any) -> MetricResult:
    wall = float(artifact.timings.get("wall_time_ms") or 0.0)
    passed = True if maximum is None else wall <= float(maximum)
    return MetricResult(
        name="wall_time_ms",
        score=1.0 if passed else 0.0,
        passed=passed,
        details={"actual": wall, "maximum": maximum},
    )


def _log_sections_complete(artifact: EvalRunArtifact) -> MetricResult:
    log = artifact.log or {}
    missing_value = log.get("missing_sections")
    parse_errors_value = log.get("json_parse_errors")
    missing = (
        missing_value
        if isinstance(missing_value, list)
        else list(REQUIRED_LOG_SECTIONS)
    )
    parse_errors = parse_errors_value if isinstance(parse_errors_value, list) else []
    passed = bool(log.get("exists")) and not missing and not parse_errors
    return MetricResult(
        name="run_log_completeness_rate",
        score=1.0 if passed else 0.0,
        passed=passed,
        details={"missing_sections": missing, "json_parse_errors": parse_errors},
    )


def _sse_sequence_valid(artifact: EvalRunArtifact) -> MetricResult:
    frames = artifact.sse_frames
    sequences = [
        frame.get("data", {}).get("sequence")
        for frame in frames
        if isinstance(frame.get("data"), dict) and isinstance(frame.get("data", {}).get("sequence"), int)
    ]
    expected = list(range(1, len(sequences) + 1))
    event_types = [frame.get("event") for frame in frames]
    passed = sequences == expected and "final_answer" in event_types and event_types[-1:] == ["run_completed"]
    return MetricResult(
        name="sse_sequence_valid",
        score=1.0 if passed else 0.0,
        passed=passed,
        details={"sequences": sequences, "events": event_types},
    )


def _tool_names(artifact: EvalRunArtifact) -> list[str]:
    return [
        str(event.get("tool_name"))
        for event in artifact.result.get("tool_events", [])
        if isinstance(event, dict) and event.get("tool_name")
    ]


def _tool_events_text(artifact: EvalRunArtifact) -> str:
    return " ".join(
        _json_text(event.get("result"))
        for event in artifact.result.get("tool_events", [])
        if isinstance(event, dict)
    )


def _filesystem_snapshot_text(artifact: EvalRunArtifact) -> str:
    snapshot = artifact.result.get("filesystem_snapshot")
    return _json_text(snapshot if isinstance(snapshot, dict) else {})


def _safety_reviews(artifact: EvalRunArtifact) -> list[dict[str, Any]]:
    reviews = artifact.result.get("safety_reviews")
    return [review for review in reviews if isinstance(review, dict)] if isinstance(reviews, list) else []


def _expected_message_ids(artifact: EvalRunArtifact, expected_external_ids: Any) -> list[str]:
    expected = _string_list(expected_external_ids)
    index = artifact.fixture_index.get("mail", {}).get("messages_by_external_id", {})
    ids: list[str] = []
    for external_id in expected:
        item = index.get(external_id) if isinstance(index, dict) else None
        if isinstance(item, dict) and item.get("message_id"):
            ids.append(str(item["message_id"]))
    return ids


def _loaded_message_ids(artifact: EvalRunArtifact) -> set[str]:
    loaded: set[str] = set()
    for event in artifact.result.get("tool_events", []):
        if not isinstance(event, dict) or event.get("tool_name") != "mail.load_messages":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        output = result.get("output") if isinstance(result, dict) else {}
        messages = output.get("messages") if isinstance(output, dict) else None
        if not isinstance(messages, list):
            continue
        for message in messages:
            if isinstance(message, dict) and message.get("message_id"):
                loaded.add(str(message["message_id"]))
    return loaded


def _mail_search_message_ids_at_k(artifact: EvalRunArtifact, k: int) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for event in artifact.result.get("tool_events", []):
        if not isinstance(event, dict) or event.get("tool_name") != "mail.search":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        output = result.get("output") if isinstance(result, dict) else {}
        messages = output.get("messages") if isinstance(output, dict) else None
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict) or not message.get("message_id"):
                continue
            message_id = str(message["message_id"])
            if message_id in seen:
                continue
            seen.add(message_id)
            ids.append(message_id)
            if len(ids) >= k:
                return ids
    return ids


def _mail_search_event_count(artifact: EvalRunArtifact) -> int:
    return len(
        [
            event
            for event in artifact.result.get("tool_events", [])
            if isinstance(event, dict) and event.get("tool_name") == "mail.search"
        ]
    )


def _mail_search_k(metric_options: dict[str, Any]) -> int:
    value = metric_options.get("mail_search_k")
    if isinstance(value, int) and value > 0:
        return value
    return 10


def _result_text(artifact: EvalRunArtifact) -> str:
    return _json_text(artifact.result.get("output", artifact.result))


def _json_text(value: Any) -> str:
    try:
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _sequence_score(actual: list[str], expected: list[str]) -> float:
    if actual == expected:
        return 1.0
    if not expected and not actual:
        return 1.0
    if not expected or not actual:
        return 0.0
    overlap = sum(
        min(_counts(actual).get(key, 0), _counts(expected).get(key, 0))
        for key in set(expected)
    )
    return round(overlap / max(len(expected), len(actual)), 4)
