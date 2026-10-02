from scripts.eval_background_compaction import evaluate


def test_actual_local_compactor_preserves_synthetic_critical_details():
    report = evaluate()
    assert report["cases"] == report["passed"] == 2
    assert report["provider_calls"] == 0
    assert report["production_slo"] is False
