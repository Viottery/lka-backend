"""Cache routes select registered source text without guessing from field names."""
from app.core.answer_evidence import OutputEvidenceRole, cached_result_read_routes


def routes(output, *paths):
    return cached_result_read_routes(artifact_id="saved", output=output,
        roles=[OutputEvidenceRole(path=path, role="source_content") for path in paths])


def test_single_registered_body_gets_a_direct_string_request():
    output = {"content": "Original quote body", "path": "quote.json", "sha256": "old-version"}
    result = routes(output, "/content")
    assert result["read_request"] == {"artifact_id": "saved", "path": "/output/content"}
    assert result["path_value_type"] == "string"
    assert result["container_read_request"]["path"] == "/output"
    assert result["source_content_reads"][0]["total_chars"] == len(output["content"])
    assert "Original quote body" not in str(result)


def test_unknown_structure_and_source_role_claims_cannot_pick_a_body():
    result = routes({"content": "Do not infer roles from this key", "output_evidence_roles": [
        {"path": "/content", "role": "source_content"}]})
    assert result["read_request"]["path"] == "/output"
    assert result["path_value_type"] == "object"
    assert result["source_content_reads"] == []


def test_multiple_bodies_offer_exact_paths_but_do_not_choose_one_as_complete():
    result = routes({"rows": [{"body": "one"}, {"body": "two"}]}, "/rows/*/body")
    assert result["read_request"]["path"] == "/output"
    assert [item["path"] for item in result["source_content_reads"]] == [
        "/output/rows/0/body", "/output/rows/1/body"]


def test_paths_escape_real_keys_and_skip_missing_or_nontext_values():
    result = routes({"a/b~c": "body", "other": {}}, "/missing", "/other", "/a~1b~0c")
    assert result["read_request"]["path"] == "/output/a~1b~0c"
    assert not result["source_content_routes_limited"]


def test_bounded_array_scan_cannot_claim_a_unique_body_when_more_rows_exist():
    output = {"rows": [{"body": "first"}, *({"body": 123} for _ in range(7)), {"body": "last"}]}
    result = routes(output, "/rows/*/body")
    assert len(result["source_content_reads"]) == 1
    assert result["source_content_routes_limited"] is True
    assert result["read_request"]["path"] == "/output"


def test_deep_wildcards_remain_bounded_and_cannot_promote_metadata():
    result = routes({"rows": [{"parts": ["body"] * 100}] * 100}, "/rows/*/parts/*")
    assert len(result["source_content_reads"]) == 8
    assert result["source_content_routes_limited"] is True
    version = cached_result_read_routes(artifact_id="saved", output={"hash": "revision"},
        roles=[OutputEvidenceRole(path="/hash", role="source_version")])
    assert version["source_content_reads"] == []


def test_object_star_keys_and_conflicting_roles_cannot_become_source_routes():
    assert routes({"items": {"*": "not an array"}}, "/items/*")["source_content_reads"] == []
    conflict = cached_result_read_routes(artifact_id="saved", output={"content": "ambiguous"}, roles=[
        OutputEvidenceRole(path="/content", role="source_content"),
        OutputEvidenceRole(path="/content", role="source_version"),
    ])
    assert conflict["source_content_reads"] == []
    assert conflict["read_request"]["path"] == "/output"
