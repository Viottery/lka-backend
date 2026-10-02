from scripts.eval_memory_recall import evaluate


def test_recall_is_cross_session_scoped_and_rejects_revoked_or_expired_records():
    report = evaluate()
    assert report["cases"] == report["passed"] == 6
    assert report["memory_enabled"]["precision"] == report["memory_enabled"]["recall"] == 1
    assert report["memory_enabled"]["false_injections"] == 0
    assert report["production_slo"] is False
