from tests.test_answer_generation_recovery_quality import answer, make_loop, response, scope


def test_final_delivery_preserves_exact_values_and_separates_internal_audit(tmp_path):
    loop, provider, manager = make_loop(tmp_path, [response("Supported finding.")])
    with scope(manager):
        assert answer(loop, []) == "Supported finding."
    prompt = provider.requests[0].messages[0].content
    assert "Preserve exact identifiers, counts, dates and units" in prompt
    assert "internal execution/audit metadata" in prompt
    assert "Lead with the requested findings" in prompt
