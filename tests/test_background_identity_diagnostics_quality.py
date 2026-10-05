"""Extracted claim hashes cannot override observed service publication state."""

import json

import pytest

from scripts import eval_runtime_background_quality as probe
from tests.test_eval_runtime_background_quality import Provider, run


@pytest.mark.parametrize(("identities", "records", "reason"), [
    ([], [], "no_valid_candidates"),
    (["first", "second"], [{"memory_id": "stable", "status": "active"}], None),
    (["same", "same"], [{"memory_id": "stable", "status": "active"}], None),
    (["first", "second"], [{"status": "candidate"}], "different_candidate_identities"),
    (["same", "same"], [{"status": "candidate"}], "no_active_record_observed; inspect records/jobs"),
])
def test_observed_promotion_takes_priority_over_raw_claim_hash_difference(identities, records, reason):
    build = getattr(probe, "extraction_identity_diagnostics", None)
    assert callable(build), "A pure service-state-aware diagnostic is required."
    result = build(identities, records)
    assert result["observed_not_promoted_reason"] == reason
    assert result["active_count"] == sum(row["status"] == "active" for row in records)
    assert result["valid_candidate_count"] == len(identities)
    assert result["same_publication_identity"] == (len(identities) >= 2 and len(set(identities)) == 1)
    assert result["identity_basis"] == "raw_claim_hash_not_service_alias_resolution"


def test_actual_worker_alias_promotion_is_not_reported_as_non_promotion(tmp_path):
    class PeriodVariantProvider(Provider):
        async def complete(self, request):
            result = await super().complete(request)
            if request.prompt_summary == "background_memory_extract":
                payload = json.loads(result.content)
                if self.extract_calls == 1:
                    payload["candidates"][0]["claim"] = probe.PREFERENCE[:-1]
                return result.model_copy(update={"content": json.dumps(payload, ensure_ascii=False)})
            return result

    report = run(tmp_path, PeriodVariantProvider())
    diagnostics = report["scenarios"]["extraction"]["identity_diagnostics"]
    assert report["mechanical_checks_pass"] and not report.get("error")
    assert diagnostics["active_count"] == 1 and not diagnostics["same_publication_identity"]
    assert len(diagnostics["unique_publication_identities"]) == 2
    assert diagnostics["observed_not_promoted_reason"] is None
