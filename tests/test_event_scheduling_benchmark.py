import pytest

from scripts.benchmark_event_scheduling import (
    _audit_batch_membership,
    _parser,
    build_attempts,
    validate_output_path,
)


def test_attempt_builder_is_reproducible_and_duplicates_are_exact():
    sessions = ["session-a", "session-b"]
    first = build_attempts(sessions, 20, seed=42)
    second = build_attempts(sessions, 20, seed=42)
    assert first == second
    assert len(first) == 20
    assert len({(item["session_id"], item["client_message_id"]) for item in first}) == 18
    grouped = {}
    for item in first:
        key = (item["session_id"], item["client_message_id"])
        grouped.setdefault(key, set()).add(item["user_input"])
        assert len(item["user_input"].encode("utf-8")) == 1024
    assert all(len(contents) == 1 for contents in grouped.values())
    assert (
        sum(
            1
            for key in grouped
            if sum((item["session_id"], item["client_message_id"]) == key for item in first) > 1
        )
        == 2
    )


def test_attempt_builder_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        build_attempts([], 10)
    with pytest.raises(ValueError):
        build_attempts(["session"], 0)


def test_output_must_be_in_ignored_engineering_directory(tmp_path):
    with pytest.raises(ValueError, match="docs/_engineering"):
        validate_output_path(tmp_path / "benchmark.json")


def test_internal_serve_parser_does_not_require_parent_output():
    args = _parser().parse_args(["--serve", "--data-dir", "/tmp/bench-data", "--port", "9876"])
    assert args.serve
    assert args.output is None


def test_production_batches_is_explicit_opt_in():
    args = _parser().parse_args(["--output", "docs/_engineering/bench.json"])
    assert args.production_batches is False
    enabled = _parser().parse_args([
        "--output", "docs/_engineering/bench.json", "--production-batches",
    ])
    assert enabled.production_batches is True


def test_batch_audit_rejects_corrupt_or_cross_session_membership():
    import hashlib

    texts = ["first message", "second message"]
    inputs = [
        {
            "input_id": f"input-{index}", "session_id": "session-a", "sequence": index + 1,
            "message_id": f"message-{index}", "run_id": "run-a", "user_input": text,
        }
        for index, text in enumerate(texts)
    ]
    batch = [
        {
            "input_id": item["input_id"], "message_id": item["message_id"],
            "sequence": item["sequence"],
            "content_sha256": hashlib.sha256(item["user_input"].encode()).hexdigest(),
        }
        for item in inputs
    ]
    runs = [{
        "run_id": "run-a", "session_id": "session-a",
        "record_payload": {
            "session_id": "session-a",
            "metadata": {"session_input_batch_frozen": True, "session_input_batch": batch},
        },
    }]
    jobs = [{"payload": {"run_id": "run-a"}}]
    assert _audit_batch_membership(inputs, runs, jobs, production_batches=True)["passed"]

    corrupt = [dict(entry) for entry in batch]
    corrupt[1]["content_sha256"] = "0" * 64
    runs[0]["record_payload"]["metadata"]["session_input_batch"] = corrupt
    audit = _audit_batch_membership(inputs, runs, jobs, production_batches=True)
    assert audit["passed"] is False
    assert audit["batch_fingerprints_match"] is False

    runs[0]["session_id"] = "session-b"
    audit = _audit_batch_membership(inputs, runs, jobs, production_batches=True)
    assert audit["passed"] is False
    assert audit["batch_same_session"] is False
